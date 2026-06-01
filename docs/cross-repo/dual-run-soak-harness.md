<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Dual-run soak harness — the per-write-op graduation gate

> The reusable 5-stage parity / state-diff / approval-completeness gate
> every Phase-C connector **write** op runs through before its consumer
> wrapper is retired. Built by
> [G11.7-T2 (#1402)](https://github.com/evoila/meho/issues/1402) under
> Initiative [#1397](https://github.com/evoila/meho/issues/1397);
> consumed by every write slice ([#1398](https://github.com/evoila/meho/issues/1398)
> / [#1399](https://github.com/evoila/meho/issues/1399) /
> [#1400](https://github.com/evoila/meho/issues/1400) and the write
> Tasks under [#1387](https://github.com/evoila/meho/issues/1387) /
> [#1388](https://github.com/evoila/meho/issues/1388)).

## Why this exists

An op being **dispatchable** is necessary but not sufficient to retire a
wrapper. The ops team's retirement scorecard (the Phase-C/D gate) and
the `govc-vs-meho-baseline` (P1–P6) require **semantic-equivalence
proof** before a wrapper is demoted. Field evidence
(`2026-05-18-meho-drove-the-op-and-the-connector-broke`) shows real ops
break subtly — a diverging plan, a state difference masked by the write
op's own success framing, a missing governance row. This harness
graduates each write op ⛔→🟡→✅ with that proof, so a wrapper is only
deleted after a clean dual-run soak with **zero unexplained diffs and
zero governance gaps**.

It does **not** build new approval infrastructure. It reuses the queue /
approve / resume substrate that shipped in v0.6.0 (#817 / #820) and the
human-queue routing + write-op secret redaction that shipped in
[#1401](https://github.com/evoila/meho/issues/1401) — it only
*dispatches* the ops under test and *reads back* the already-shipped
audit, broadcast, and READ surfaces.

## The two halves

| Half | Lives in | Owns |
| --- | --- | --- |
| **Decision core** | [`backend/scripts/soak_harness.py`](../../backend/scripts/soak_harness.py) | Every comparison rule (parity diff, cosmetic-noise normalisation, state diff, idempotency drift, the #817 approval-completeness invariant) + the scorecard-cell derivation. Unit-tested, connector-agnostic, the same across every slice. |
| **Driver** | [`scripts/soak/soak-harness.sh`](../../scripts/soak/soak-harness.sh) | Marshals a per-op **evidence bundle** into the decision core and renders the verdict + exit code. Consumer-extensible. |

The split matters: the comparison rules are where the subtle bugs hide,
so they live in one tested place rather than scattered across N
connector `parity-check-<connector>.sh` scripts. This harness **extends**
the consumer's `parity-check-<connector>.sh` (the P6 step) — the
connector script produces the evidence bundle; the driver grades it.

## The 5 stages

| # | Stage | Automatable? | Where |
| --- | --- | --- | --- |
| 1 | **Dry-run / read-back parity** — MEHO resolves the same target + params + **plan** as the wrapper (`kubectl apply --dry-run=server` vs `meho k8s apply --dry-run`; VCF task-preview / DRS recommendation). Diverging plans fail before anything writes. | ✅ `parity_diff` | core |
| 2 | **Dual-run on a disposable target** — both wrapper and MEHO op run against the same scratch target (holodeck, k3d/CI cluster, scratch Vault path), same operator workflow. Capture each side's **effect**, not stdout. | Orchestrated (driver + consumer hooks) | driver |
| 3 | **State diff, not framing** — read post-op state back via the **already-shipped READ ops**, normalising cosmetic diffs (timestamps, generated UIDs, MEHO's reduced envelope). Idempotent ops run twice to prove no drift. Any semantic divergence is a blocker with recorded rationale. | ✅ `state_diff` + `idempotency_drift` | core |
| 4 | **Audit + broadcast + approval completeness** — exactly one **dispatch audit row** (`path == op_id`, the durable write-record the dispatcher writes once the op executes) + one broadcast event AND (being `dangerous` + `requires_approval`) exactly the **two synchronous approval audit rows** (`approval.request` + `approval.decision`, the #817 invariant), with the op not returning until the decision row commits. A rejected decision never executes → zero dispatch rows + zero write broadcasts. Redacted-class ops never leak a credential to the feed. | ✅ `assert_approval_completeness` | core |
| 5 | **Bounded live soak** — MEHO + wrapper in parallel on the **real** target for a bounded window (~2 weeks / N≥10 real invocations per op), wrapper authoritative. The scorecard write column moves 🟡→✅ only after a clean soak. | Documented protocol (below) | runbook |

## The evidence bundle (consumer wiring contract)

Each connector slice supplies a `parity-check-<connector>.sh` that drives
its op against the scratch target (stage 2) and writes these files into
an `--evidence-dir`, **before** invoking the driver:

| File | Stage | Shape |
| --- | --- | --- |
| `wrapper-plan.json` | 1 | the wrapper's dry-run / server-preview output |
| `meho-plan.json` | 1 | `meho … --dry-run` output |
| `wrapper-state.json` | 3 | post-op read-back of the wrapper's effect (via a READ op / `kubectl get -o json` / etc.) |
| `meho-state.json` | 3 | post-op read-back of MEHO's effect (via the **shipped MEHO READ op**) |
| `meho-state-2.json` | 3 | (only with `--idempotent`) read-back after a **second** MEHO run |
| `audit-rows.json` | 4 | array of `audit_log` rows for the op window — the two `approval.*` rows **and** the dispatch row with `path == op_id` (`[{path, operator_sub}, …]`) |
| `broadcast-events.json` | 4 | array of broadcast events for the window (`[{op_id, op_class, payload}, …]`) |
| `meta.json` | 4 | `{"returned_after_decision": bool, "decision": "approved"｜"rejected"}` |

Read-back must use the **already-shipped READ ops**, never the write op's
own return value — that is the stage-3 "state diff, not framing" rule.

## Running the harness

```bash
scripts/soak/soak-harness.sh \
  --op k8s.scale --connector k8s-1.x \
  --evidence-dir ./soak-evidence/k8s.scale \
  [--idempotent] [--soak-clean] [--explained-file ./explained.json]
```

* `--idempotent` adds the stage-3 double-run leg (for `snapshot.revert`,
  `kv.put` with the same value, `namespace.create`, …).
* `--explained-file` is a JSON object mapping a dotted state-diff path to
  a rationale; those divergences are recorded `explained` (visible, not
  blocking) — e.g. MEHO deliberately omits a deprecated annotation the
  wrapper still writes.
* `--soak-clean` is the operator's attestation that **stage 5 ran clean**
  (see below). Only pass it after reviewing the live-soak log.

Exit codes: `0` = automatable stages clean (op is at least 🟡), `1` = a
blocker (op stays ⛔, wrapper must not be retired), `2` = usage /
environment error. The full per-stage verdict is written to
`<evidence-dir>/soak-report.json`.

A worked, runnable example bundle ships at
[`scripts/soak/examples/k8s.scale/`](../../scripts/soak/examples/k8s.scale)
and is CI-exercised end-to-end by
[`backend/tests/test_soak_harness_driver.py`](../../backend/tests/test_soak_harness_driver.py).

## Stage 5 — the bounded live-soak protocol

Stages 1–4 graduate an op ⛔→🟡 in a single run. Stage 5 is the
wall-clock proof that moves it 🟡→✅. It is a **protocol**, not a single
command, because it runs against the real target over a bounded window.

1. **Pick the soak posture per op:**
   * *Observe-only* ops (reads, idempotent reverts) — run MEHO in
     **shadow**: the wrapper stays authoritative; MEHO runs in parallel
     and its effect/state is compared but never relied on.
   * *Must-mutate* ops — run **MEHO-primary + wrapper-verify**: MEHO
     performs the write, the wrapper (or a READ op) verifies the effect
     immediately after.
2. **Run for the bounded window:** ~2 weeks **or** N≥10 real operator
   invocations of the op, whichever is later. Every invocation runs the
   stages-1–4 driver on its evidence bundle and appends the
   `soak-report.json` to a per-op soak log.
3. **Triage every diff:** any blocker from any invocation either gets a
   recorded rationale (added to `--explained-file`, re-graded) or stops
   the soak — the op drops back to ⛔ and the slice is fixed.
4. **Graduation criterion:** the op is ✅ retirement-ready only after the
   window completes with **zero unexplained diffs and zero governance
   gaps** across every invocation. The operator then re-runs the driver
   with `--soak-clean` to emit the ✅ cell.

## Updating the retirement scorecard

The harness never edits the scorecard (that is an ops-repo action). It
emits the cell its evidence **supports**; the operator transcribes it.

1. Run the driver for the op. Read `supported_scorecard_cell` from
   `soak-report.json`:
   * `blocked` → leave the write column at ⛔. Do **not** retire the
     wrapper. Fix the blocker (`soak-report.json` lists each one) and
     re-run.
   * `shadow` → move the write column to 🟡 and **start the stage-5 soak**.
   * `ready` → move the write column to ✅ (only reachable by re-running
     with `--soak-clean` after a clean stage-5 window).
2. Record the `soak-report.json` (or its path/commit) alongside the
   scorecard cell so the demotion is auditable — the report is the proof
   the cell rests on.
3. Only after the cell reads ✅ for an op is its wrapper verb eligible for
   deletion in the consumer repo (the Phase-D wrapper-retirement step).

## Reference run

[`backend/tests/test_soak_harness_reference_run.py`](../../backend/tests/test_soak_harness_reference_run.py)
is the reference application of the harness against **live backplane
primitives**. #1398 (the real k8s write ops) is not yet merged, so the
reference run drives a stand-in `requires_approval=True` write op through
the full **human** queued → approve → resume cycle that #1401 shipped,
captures the real `approval.request` / `approval.decision` audit rows and
the real broadcast events, and feeds them into the stage-4 verifier —
proving the governance invariant holds against the actual substrate. When
#1398 merges, the consumer's `parity-check-kubernetes.sh` produces the
same evidence shape against the real `k8s.scale` op and this same driver
grades it; no harness change is required.

## Worked retirement — `host.detach_from_vds` (the headline VCF retirement)

Initiative [#1400](https://github.com/evoila/meho/issues/1400) wires the
8 VCF write composites onto this harness. The first op to soak is
`vmware.composite.host.detach_from_vds`, and it is the **highest-value
retirement of the whole initiative** for one reason:

> **`govc` cannot express it.** Every other composite has a `govc`
> equivalent the operator falls back to. `host.detach_from_vds` does
> not — there is no `govc host.detach-from-vds` verb. The consumer's
> `scripts/host-detach-from-vds.py` exists *precisely because* govc
> could not do the per-VM NIC migration + DVS `remove_host` sequence in
> one safe, ordered step. Retiring that script is the one place this
> initiative removes a capability gap rather than a convenience wrapper
> — so it soaks first and graduates first.

### What the composite does

`host.detach_from_vds` (`safety_level="dangerous"`,
`requires_approval=True`) lists the DVS portgroups + VMs on a host,
migrates each VM's NICs off the DVS to the supplied `fallback_network`
via `PATCH:/vcenter/vm/{vm}/network`, then dispatches
`POST:/vcenter/network/dvs/{dvs}?action=remove_host`. vSphere refuses
the detach while any VM still has an active NIC on the DVS, so the
composite verifies every NIC migrated before attempting the detach; on
partial migration it returns `status="incomplete"` and skips the
detach. Parameters: `host`, `dvs`, `fallback_network` (all required).

### The committed evidence bundle

A worked, runnable bundle ships at
[`scripts/soak/examples/host.detach_from_vds/`](../../scripts/soak/examples/host.detach_from_vds)
and is CI-exercised end-to-end by
[`backend/tests/test_soak_harness_host_detach_from_vds.py`](../../backend/tests/test_soak_harness_host_detach_from_vds.py).
It is the VCF analogue of the `k8s.scale` example: the same seven-file
contract, populated with realistic vSphere shapes —

* **stage 1** — `wrapper-plan.json` / `meho-plan.json`: the planned
  per-VM NIC migrations + the `remove_host` detach. MEHO's plan omits
  the vSphere task envelope (`taskId` / `startTime`) the wrapper carries;
  those keys normalise away, so the two plans grade as semantic parity.
* **stage 3** — `wrapper-state.json` / `meho-state.json`: the post-op
  read-back (host DVS membership `false`, each VM's NIC re-homed on
  `network-9`, zero DVS proxy switches left on the host), read via the
  **shipped READ ops** — not the write op's own `status` framing. The
  two runs differ only in fresh `taskId` / `endTime`, which strip away.
* **stage 4** — `audit-rows.json` / `broadcast-events.json` /
  `meta.json`: the two `approval.*` rows bracketing the single
  `path == "vmware.composite.host.detach_from_vds"` dispatch row, one
  write broadcast event, `returned_after_decision: true`.

Run it:

```bash
scripts/soak/soak-harness.sh \
  --op vmware.composite.host.detach_from_vds --connector vmware-rest-8.x \
  --evidence-dir scripts/soak/examples/host.detach_from_vds
```

The clean bundle grades 🟡 **SHADOW** (stages 1–4 pass; the op is ready
to *enter* the live soak, not yet retirement-ready). The test also
proves the two failure classes the harness exists to catch: a NIC
migrated to the **wrong** `fallback_network` (a silently diverging plan
that would strand a VM once the host loses DVS connectivity) and a
missing dispatch audit row both drop the cell to ⛔ **BLOCKED**.

### Running the live dual-run soak (stage 5, operator action)

The committed bundle proves stages 1, 3, and 4 against a static fixture.
The 🟡→✅ promotion requires the **live holodeck dual-run** — an operator
protocol, not a CI step, because it needs a real PowerShell-over-SSH
ESXi host and a real vDS. On the holodeck lab:

1. Stand up a **disposable** host on a scratch DVS with ≥2 VMs whose
   NICs sit on a DVS portgroup, plus a standard-switch fallback network.
2. For each soak invocation, run both paths against the same scratch
   host (the consumer's `scripts/host-detach-from-vds.py` and
   `meho vmware composite host detach_from_vds`), capture each side's
   plan + post-op read-back into the bundle file shapes above, and run
   the driver. Wrapper stays authoritative for the window.
3. Soak for the bounded window (~2 weeks **or** N≥10 real detaches,
   whichever is later — see *Stage 5* above). Triage every diff: a
   blocker either earns a recorded rationale (`--explained-file`) or
   stops the soak and the slice is fixed.
4. **Pass criterion:** the window completes with **zero unexplained
   diffs and zero governance gaps** across every invocation. The
   operator then re-runs the driver with `--soak-clean` to emit the ✅
   READY cell.

### Cross-repo handoff — queue the script for Phase-D deletion

`scripts/host-detach-from-vds.py` lives in the consumer/ops repo
[`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc),
**not** in `evoila/meho` — its deletion is a cross-repo operator action,
tracked but not performed here. Once the soak grades ✅ READY:

1. File the wrapper-retirement issue on the consumer repo (`Retire
   scripts/host-detach-from-vds.py — superseded by
   meho vmware composite host detach_from_vds`), linking the ✅
   `soak-report.json` as the proof the cell rests on and this section
   as the methodology.
2. Move the `host.detach_from_vds` write column to ✅ on the ops team's
   retirement scorecard (see *Updating the retirement scorecard* above).
3. The consumer deletes `scripts/host-detach-from-vds.py` in its Phase-D
   wrapper-retirement PR. **Only `evoila/meho`'s side is in scope here**
   — this repo never edits the consumer repo.

### Out of scope — NSX and SDDC Manager **writes**

This initiative activates **vSphere/vCenter** write composites only. NSX
and SDDC Manager stay **read-only-curated**: their READ ops are ingested
and useful, but **no net-new write composite is authored** for them yet.
Rationale: the headline operator pain this initiative removes is the
vSphere wrapper set (govc + the `host-detach-from-vds.py` gap), and
there is no comparable retirement-driving demand for NSX/SDDC writes —
authoring dangerous write surfaces for them without a wrapper to retire
would add governance + soak burden with no offsetting wrapper removal.
NSX/SDDC write composites are a deliberate follow-up, gated on their own
demand signal, not part of #1400.

## Related

* Initiative [#1397](https://github.com/evoila/meho/issues/1397) — the
  thin approval-policy layer this harness sits on.
* [#1401](https://github.com/evoila/meho/issues/1401) — human-queue
  routing, self-approval guard, write-op secret redaction
  (`credential_write` / `credential_mint`) the stage-4 check asserts.
* [#817](https://github.com/evoila/meho/issues/817) / #820 — the durable
  approval queue + dispatch-time gate (v0.6.0) the two-row invariant
  comes from.
* [`docs/codebase/approvals.md`](../codebase/approvals.md) — the approval
  substrate internals.
* The ops team's retirement scorecard + `govc-vs-meho-baseline` (P1–P6)
  — the methodology source, in the consumer/ops repo.
