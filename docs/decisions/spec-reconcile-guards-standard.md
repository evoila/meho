# Spec-reconcile guards as standard — every hand-coded vendor path asserts against a pinned spec (decision)

**Status:** accepted
**Date:** 2026-08-17
**Task:** [#2980](https://github.com/evoila/meho/issues/2980) (initiative
[#2979](https://github.com/evoila/meho/issues/2979), wave 3 of the guard
rollout; mechanics precedent
[#2944](https://github.com/evoila/meho/issues/2944) /
[#2970](https://github.com/evoila/meho/issues/2970))

## The standard

**Every hand-coded vendor-API path literal in a typed connector or
composite MUST be asserted against a pinned vendor spec in CI, wherever
such a spec exists or can be pinned.**

The determination of record behind it (#2979, grounded 2026-08-17): *if
we have the spec, we do not risk hallucinating an endpoint.* #2970
checked 13 hand-coded vmware composite paths against the real pinned
`vcenter.yaml` — **11 were not served** (typos, renamed endpoints, wrong
API versions). Fixture-based tests cannot catch this defect class: they
synthesise their fixture *from* the same constants they check, so they
prove op_id **shape**, never real-path **existence**. Only the vendor's
own pinned spec answers "does this path actually exist?" on every PR;
otherwise the answer arrives from a live system, in production.

Scope boundaries:

- **Typed connectors and composites** are the exposure: their
  `METHOD:/path` literals are hand-written and nothing else validates
  them. Enumerate them by **introspecting the live constants** (the
  #2944 pattern — never a hardcoded mirror that can drift).
- **Generic (ingested) connectors are immune by construction** and need
  no lane: their op_ids are emitted *from* the spec by the ingest
  pipeline, so an unserved path cannot exist.
- **Where no spec exists or can be pinned**, the lane still ships and
  skips uniformly; it arms itself the day the spec lands on the shelf.
  A task that proves a spec is unobtainable records the evidenced
  exclusion instead (#2993's contract; the entries live in the
  [Evidenced exclusions](#evidenced-exclusions-no-pinnable-spec-today)
  section below). Cautionary precedent: `nsx-9.0` shipped as exactly
  such an exclusion and was falsified in review — its 30-path probe
  never covered the vendor-documented spec endpoints (see the
  activation record below). An exclusion's evidence must cover every
  endpoint family the vendor documents, not just conventional guesses.

## The harness (what a lane is)

`backend/tests/_spec_shelf.py` (#2980) generalises the vcenter-only
resolver (`tests/acceptance/_vcenter_spec.py`, which now delegates its
shelf-root branch to it) into three calls; a complete lane is
`backend/tests/test_spec_shelf_example_lane.py` (<30 lines):

```python
spec_path = require_shelf_spec("<product>-<version>", "<spec-file>")
served = openapi_served_op_ids(spec_path)          # real ingest parser
assert_op_ids_served(declared, served, spec_label="<product>-<version>/<spec-file>")
```

- `require_shelf_spec` resolves
  `$MEHO_CONSUMER_DOCS_ROOT/<product-dir>/<file>` or `pytest.skip`s
  with a uniform reason naming the exact missing file — env unset,
  shelf root missing, product dir absent (sparse checkout not yet
  widened), and file absent all skip identically. CI sets
  `MEHO_CONSUMER_DOCS_ROOT` unconditionally; the secret-gated checkout
  decides whether the directory exists.
- `openapi_served_op_ids` runs the pinned spec through the real
  `parse_openapi`, so served op_ids are byte-for-byte the
  `endpoint_descriptor.op_id` strings dispatch pre-flight resolves
  against — including vCenter-style `?action=` path-key suffixes. Union
  the sets when a product pins multiple specs.
- `assert_op_ids_served` fails with an actionable diff: each unserved
  `METHOD:/path` plus up to three near-miss candidates from the served
  set (shared trailing resource name — the #2970 repoints were exactly
  this shape: right resource, wrong API-family prefix). An empty
  declared set **fails** — a lane that enumerates nothing guards
  nothing.

Non-OpenAPI spec artifacts (e.g. `hetzner-robot-2026-04`'s vendored
webservice markdown) still use `require_shelf_spec` for resolution and
skip semantics; the lane supplies its own served-set extraction and
feeds `assert_op_ids_served` as usual.

## Lane placement (CI budget)

Two cost classes of shelf-backed test, two CI homes:

- **Reconcile lanes** (this standard's subject, every #2981-#2993
  lane): parse the pinned spec, set-compare op_ids. No DB, no
  embeddings, no containers — **seconds** when armed. They live in the
  required unit sweep (`python-lint-test`) like any unit test, and
  need **no gating of any kind**.
- **Full-ingest canaries** (today: the G0.7 vSphere canary,
  `tests/acceptance/test_g07_vsphere_canary.py`): drive the whole
  ingest pipeline — Postgres testcontainers, real embeddings, the
  grouping pass — over the full pinned corpus. **Minutes per lane**
  when armed (a full two-spec ingest measures ~164 s on CI runners,
  amortised once per module — see the shared-corpus pattern below).
  They run in exactly one armed lane: `python-integration` (its pytest
  step selects the canary module alongside `tests/integration/`), and
  the unit sweep opts out via `MEHO_SKIP_SPEC_INGEST_TESTS=1`, honored
  by a collection-time `skipif` marker on every full-ingest test (so
  an opted-out lane never even spins the module-scoped Postgres
  container), with backstop re-checks in the canary's shared
  `_canary_corpus` fixture and its `vcenter_spec_path` fixture.
  Everywhere the var is unset (local dev, the integration lane) the
  armed-shelf contract is unchanged.

Why this split is load-bearing: the day the shelf was first armed
(2026-08-17, #2949/#2966), the canary's full ingest ran inside the
unit sweep for the first time and took the job from a 478 s pytest
wall past its 25-min cap — four consecutive timeout kills, the last on
a quiet cluster with pytest progressing. `ci.yml`'s perf-budget
discipline (#898/#793/#827) applies: measure and relocate the slow
path; never just raise `timeout-minutes` or `budget_s`. A future lane
that performs a full ingest (rather than a parse-only reconcile)
inherits the canary's placement; a reconcile lane never needs it.

### Shared-corpus amortisation (the pattern for heavy ingest lanes)

Relocation alone is not enough when the heavy work repeats per test.
The canary's first armed run in `python-integration` (PR #2995) proved
it: a function-scoped ingest fixture re-ran the ~164 s two-spec ingest
for each of 25 armed tests — ~68 min of ingest against a 60-min job
cap, a timeout kill with zero individual test being slow. The fix of
record is **amortise, never raise `timeout-minutes`**:

1. **One module-scoped corpus fixture** performs the expensive ingest
   exactly once (`_canary_corpus` in the canary module). It is a
   *sync* fixture that runs the async pipeline via `asyncio.run` on a
   private event loop and disposes every DB resource inside that loop
   — module-scoped *async* fixtures would couple to pytest-asyncio
   loop-scope config, and an engine's pooled connections must never
   cross event loops. Because module-scoped fixtures instantiate
   before function-scoped autouse fixtures, the corpus fixture pins
   its own env (chassis vars, `DATABASE_URL`, model cache) via a
   module-lifetime `pytest.MonkeyPatch` and brackets its own log
   silencing.
2. **A thin function-scoped binder** (`ingested_canary`) re-pins a
   fresh per-test engine to the same module-scoped container WITHOUT
   truncating. This is the piece that makes module scope survivable:
   the repo's per-test DB fixtures (`pg_engine` in both the
   integration and acceptance conftests) TRUNCATE every chassis table
   between tests — exactly what would wipe a shared corpus — and the
   root conftest's autouse `_default_database_url` disposes/resets the
   process engine cache after every test, so a per-test re-pin is
   mandatory regardless.
3. **Read-only isolation contract.** Every consumer of the shared
   corpus must be read-only against it. A test that mutates connector
   state keeps a function-scoped ingest against the truncating
   `pg_engine` (the canary's env-gated vcsim-dispatch and real-LLM
   variants are the reference shape).

Measured effect: the full armed canary module dropped from a projected
~80 min (25 ingests + lane baseline, past the 60-min cap) to ~90 s
locally / a projected ~15-17 min total integration-lane wall. A future
heavy lane (full ingest over a large pinned corpus, N > a few tests)
starts from this pattern; per-test ingest is only acceptable while
`N × ingest cost` stays trivially inside the lane budget.

## Extension mechanics (per new vendor lane)

Each lane task (#2981-#2993) ships all of:

1. **The lane** — a `backend/tests/` module using the harness;
   declared set introspected from the connector's live constants.
2. **Spec pinned on the shelf** (when not already there) — a PR to the
   consumer shelf repo adding `docs/<product>-<version>/<spec-file>` +
   `MANIFEST.md` provenance. Freely-licensed (OSS) specs need only that
   provenance note; vendor-licensed specs follow the signoff model
   below.
3. **CI checkout widened** — one line per job in
   `.github/workflows/ci.yml`: add the product dir to the spec-shelf
   `sparse-checkout` list, and one line in the fail-loud verify step so
   a shelf-layout move fails the job instead of silently regressing the
   lane to skip. Same `SPEC_SHELF_TOKEN`; **no new secret, ever** — the
   PAT already reads the whole private shelf repo. These one-liners
   collide trivially across parallel lane tasks; rebase, don't battle.
4. **Signoff extended** — one paragraph per vendor appended to
   [`vendor-spec-ci-provisioning.md`](vendor-spec-ci-provisioning.md)'s
   signoff of record (what is fetched, same ephemeral-use conditions).
   OSS-licensed specs need only the provenance note from step 2
   referenced there.
5. **Local pre-verify** — run the lane against a full local shelf
   (`MEHO_CONSUMER_DOCS_ROOT=<shelf>/docs`) before merge. CI is armed:
   a lane that would go red lands red on the PR itself, which is the
   next section — but discovering it locally first is cheaper.

## Evidenced exclusions (no pinnable spec today)

The per-product record for lanes that ship **dormant** because no vendor
spec exists or can be pinned. Each entry states what was checked (with
dates), why nothing is pinnable, and what activates the lane. An entry
here is not an exemption from the standard — the lane still ships,
skipping uniformly, and the day a spec lands the activating task runs
the full extension mechanics above (CI checkout widened, signoff
extended, first-run red triaged per the protocol below).

### nsx-9.0 — **ACTIVATED 2026-08-18** (#2981; exclusion withdrawn, retained as history)

This entry shipped on 2026-08-17/18 as an evidenced exclusion ("no
pinnable NSX 9 spec"). Review of PR #3007 (blocker B1) falsified it:
the exclusion's probe record had a **coverage gap**, and the specs are
pinnable. The record below replaces the exclusion; the lane is armed.

- **Lane:** `backend/tests/test_connectors_nsx_spec_reconcile.py` — 11
  hand-coded op_ids introspected live (ten `GET` typed-read paths +
  the session-establish `POST`), asserted against the **union** of the
  two pinned specs per the harness's multi-spec guidance.
- **Why the exclusion fell.** The 2026-04-29 evidence (30 candidate
  spec endpoints probed 404/302 on a live NSX 9.0.2 manager under both
  auth flows; no public artifact in `vcf-api-specs` / `vmware-nsx` /
  `go-vmware-nsxt` / `nsxraml`) was real but incomplete: **none of the
  30 paths covered the vendor-documented
  `/api/v1/spec/openapi/nsx_*` family** the NSX REST API portal
  (`developer.broadcom.com/xapis/nsx-t-data-center-rest-api/latest/`)
  actually names. Probed 2026-08-18 against the live lab manager
  (`c2fs1-nsx` / `nsx-mgmt-01a`, NSX 9.1.0.0.25318225, session-auth):
  `GET /api/v1/spec/openapi/nsx_api.json` (200; 4,417,173 B),
  `nsx_api.yaml` (200; 5,429,995 B), `nsx_policy_api.json` (200;
  12,195,576 B), `nsx_policy_api.yaml` (200; 14,683,229 B). The public
  half of the negative stands (no public artifact); the "live-manager
  fetch is closed" half was never evidenced and is false. The shelf's
  probe-history records carry dated corrections
  (`kb/nsx-9.0-overview.md`, `kb/vcf-9.0-in-ui-explorer-survey.md`).
- **What is pinned** (shelf `nsx-9.0/MANIFEST.md`, full provenance +
  sha256): `nsx_api.json` (Manager API, Swagger 2.0, `basePath:
  /api/v1`, 1,193 paths) and `nsx_policy_api.json` (Policy API,
  `basePath: /policy/api/v1`, 2,317 paths) byte-identical as fetched,
  plus deterministic OpenAPI 3.0 conversions
  (`nsx_api.openapi3.json` / `nsx_policy_api.openapi3.json` —
  swagger2openapi@7.0.8, placeholder `host`/`schemes` stripped so the
  relative basePath lands in `servers[0].url` for the #1796 fold,
  minified under ingest's 20 MiB cap). The lane pins the conversions:
  `parse_openapi` accepts 3.0/3.1 only and documents conversion as the
  Swagger-2.0 remediation, so they are byte-for-byte what an operator
  ingest consumes.
- **First-run reconcile findings (the protocol working), all
  dispositioned in PR #3007:**
  1. `GET:/api/v1/transport-nodes/{id}/state` — unserved; vendor
     template is `{transport-node-id}`. **Mechanical rename** of the
     constant's template segment (anticipated verbatim by the dormant
     entry).
  2. `GET:/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones`
     — unserved; the constant hard-coded the `default`/`default`
     instantiation of the served template
     `…/sites/{site-id}/enforcement-points/{enforcementpoint-id}/transport-zones`.
     **Mechanical**: constant now carries the vendor template; the
     call site instantiates `default`/`default` — runtime request path
     byte-identical.
  3. `POST:/api/session/create` — the Manager API spec models the
     session endpoints as path keys under `basePath: /api/v1`, so the
     ingested op_id is `POST:/api/v1/api/session/create`. Live probe
     2026-08-18: **both** concatenations serve (the canonical
     documented `/api/session/create` is the working session-auth
     flow; the base-folded path returns 400 on an empty body where
     garbage-path controls return 404). **Not a repoint** — the
     connector keeps the canonical endpoint; the lane translates via
     its documented `_SPEC_MODELED_OP_IDS` map. A repoint of working
     production auth to satisfy a literal comparison would invert the
     protocol's "never invent a path" rule.
- **Extension mechanics run** (per the section above): CI
  sparse-checkout + fail-loud verify widened to `docs/nsx-9.0` in both
  Python jobs; signoff extended in
  [`vendor-spec-ci-provisioning.md`](vendor-spec-ci-provisioning.md)
  (vendor-licensed form — NSX manager-served content is fetched into
  ephemeral CI); local pre-verify green against a full local shelf.
  **Sequencing:** the shelf pin lands via consumer-repo PR
  (`claude-rdc-hetzner-dc#2514`) which must merge before this repo's
  widened verify step can pass.

## Red lane = finding (the protocol)

A red lane on first run against the real shelf is **the guard working**,
not harness noise — #2944 anticipated it verbatim ("a shelf-backed red
is the guard surfacing a real finding … not a reason to withhold the
guard") and #2970 confirmed it at 11/13. The protocol, per finding:

1. **Never invent a path.** Every repoint must target a path the pinned
   spec actually serves — grep the shelf's `*.paths.txt` / the spec
   itself; the assert's near-miss hints are leads, not answers.
2. **Mechanical fixes land in the same PR** (typo, plural/singular,
   renamed segment), one per-op decision with rationale in the PR body
   — the #2970 shape.
3. **Design-level repoints split into a fix task** (e.g. the surface
   only exists on another API family, as when #2970 moved snapshot /
   maintenance / DRS surfaces from REST to vi-json): the lane PR
   documents the red, the fix task re-points, the lane merges green
   after it.
4. **Never merge red.** Both reconcile-lane hosts are required
   merge-gate jobs; a red lane on `main` turns every PR red.

## Consequences

- The five vcenter lanes are unchanged in behavior: same env-var
  priority, same skip reason, same CI wiring (`_vcenter_spec.py` keeps
  its public API and explicit/legacy env vars; only its shelf-root
  branch delegates to the shared resolver).
- New-connector reviews gain a checkable rule: a PR adding a hand-coded
  `METHOD:/path` literal for a shelf-pinned product must extend that
  product's lane (or the lane's introspection must already sweep it in).
- The example lane doubles as the harness's CI-armed integration proof:
  it runs against the real `vcenter-9.0` shelf on every same-repo PR.

## References

- Harness: `backend/tests/_spec_shelf.py`; example lane:
  `backend/tests/test_spec_shelf_example_lane.py`; harness unit tests:
  `backend/tests/test_spec_shelf_harness.py`.
- Lane pattern of record:
  `backend/tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`
  (#2944); findings precedent #2970.
- Shelf provisioning, licensing record, secret runbook:
  [`vendor-spec-ci-provisioning.md`](vendor-spec-ci-provisioning.md).
- Initiative: [#2979](https://github.com/evoila/meho/issues/2979)
  (per-vendor lanes #2981-#2993).
