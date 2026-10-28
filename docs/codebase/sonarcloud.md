<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# SonarCloud — how it's wired, how to read it, how to keep it honest

MEHO is analysed by [SonarCloud](https://sonarcloud.io/project/overview?id=evoila_meho)
(project `evoila_meho`, organisation `evoila`, **public** project — read APIs
need no token). This doc is the operator runbook: what the analysis covers, how
the new-code period and scan trigger are configured, and how to triage results
without drowning in false positives.

## How analysis runs

```
nightly schedule (06:17 UTC) ──► CI workflow on main
                                   │   8 python-coverage shards + python-coverage-combine
                                   │   upload the python-coverage artifact
                                   │   (backend/coverage.xml)          [schedule only]
                                   │
                                   └─ on success ──► Quality Gate workflow (workflow_run,
                                                       event == 'schedule' only)
                                                        ├─ downloads python-coverage into backend/
                                                        └─ SonarSource/sonarqube-scan-action ──► SonarCloud
```

The coverage artifact is produced **once a night**, by the `schedule` trigger
in `ci.yml` (#4910), through a dedicated non-required `python-coverage` job —
NOT on push to `main`, NOT on PRs, NOT in the merge queue, and NOT by the
required unit lane. SonarCloud therefore gets one analysis a day, of the
`main` tip at that time. Until #4910 the coverage shards ran after every
merge: 8 heavy-pool jobs per push, about 3,000 heavy-runner minutes a day,
most of it cancelled by the next merge, and the merge queue's unit shards
waited for free runners. A PR that changes the coverage path itself can
still run the shards before merge with the `ci-coverage-validate` label.
Why coverage is not in the required unit lane: running the unit suite under
coverage peaks the pytest tree at
~12.5–14 GiB (vs ~7.9 GiB no-cov), which OOM-killed the memory-limited
`meho-runners-ci-heavy` pod when coverage rode on the required lane (#1982). The
coverage tax is intrinsic to measuring the whole `meho_backplane` package across
the ~8.3k-test suite and barely moves with xdist worker count — see #1987 for the
peak-memory measurement table. The coverage job is **job-level
`continue-on-error: true`** so an OOM degrades to "no coverage for this push" and
never fails the CI run conclusion (which would also suppress this gate's
`workflow_run` trigger). A pytest *hang* degrades the same way, guarded in three layers
(#2800 → #2806 → #2865). The **primary** guard is in-process: the coverage pytest
step runs under pytest-timeout (`--timeout=300 --timeout-method=signal
--max-worker-restart=0`), so a wedged test dies as a `Failed` carrying its own
traceback after 300 s — from inside the pytest process, independent of runner
health. #2865 is why in-process is primary: on the v0.28.0 cut a pytest hang
outlived the step `timeout-minutes` because the runner itself was wedged (step 9
still `in_progress` at the ~60-min job kill, run 31199593016 cancelled 2×), so a
runner-enforced timeout is not a reliable backstop for a pytest hang. `signal`
(not `thread`) is deliberate under `-n 1` xdist: thread's `os._exit()` reads as a
worker crash that xdist re-runs up to `--max-worker-restart` times (~5×, one 300 s
hang ballooning into a job-cap cancel) and whose stack dump xdist swallows;
`--max-worker-restart=0` caps any *other* worker death (segfault / OOM-kill) at
zero retries. The step's own `timeout-minutes: 45` (step timeout → step failure →
absorbed) is the layer-2 backstop for a collection stall or a GIL-locked C call
the per-test SIGALRM cannot reach; the job-level cap (55 min) is the outer
backstop for a wedged non-pytest step. All three keep the run conclusion
`success` — `continue-on-error` absorbs *failure* only, and a job-level timeout
expiry *cancels* the job, whose `cancelled` conclusion leaks through and
suppresses this gate just like a failure would (#2800, observed 2026-08-04). On
any hang the step's pytest output is uploaded as the `coverage-pytest-log`
artifact so the next hang says *where* it hung.

| Concern | Where |
|---|---|
| Project + scope config | [`sonar-project.properties`](../../sonar-project.properties) |
| Scan trigger + coverage handoff | [`.github/workflows/quality-gate.yml`](../../.github/workflows/quality-gate.yml) |
| Coverage production | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — `python-coverage` shards + `python-coverage-combine` (nightly `schedule` run only, offline `coverage combine`+`xml`) |
| Coverage config (CI offline-combine) | [`backend/.coveragerc.ci`](../../backend/.coveragerc.ci) — `parallel`, `source`, `relative_files` |
| Coverage path portability | `[tool.coverage.run] relative_files` ([`backend/pyproject.toml`](../../backend/pyproject.toml)) + the same key in `.coveragerc.ci` + quality-gate.yml's `Point coverage report at the backend/ source root` step (injects `<source>backend</source>` so paths resolve from the repo root) |
| Auth | `SONAR_TOKEN` repo secret (write/scan only) |

Coverage is the **unit sweep only** (`--cov`-equivalent over `meho_backplane`);
integration tests run in a separate job without coverage. The gate is
`continue-on-error: true` — it is **advisory** and never blocks merges (see the
new-code-period + scan-trigger section below for why it is kept advisory).

## Measuring a test-to-code map (#5298)

`scripts/ci/measure_test_map.py` counts how many tests a "run only the tests
for the changed code" map would pick for recent merged pull requests. CI does
not use it. It needs coverage data that records which test ran each line:
`dynamic_context = test_function` in `.coveragerc.ci`. That setting is **not**
on `main`. To get the data, open a pull request that adds it, give it the
`ci-coverage-validate` label, and keep the combined `.coverage` file from the
combine job (it is not uploaded today, only `coverage.xml`). How to run the
script is in its docstring.

The script also needs the list from `pytest --collect-only`. Make that list
on the **exact commit the coverage run tested**. For a `pull_request` run this
is the merge commit, not the pull request head. The log of the checkout step
shows its SHA ("HEAD is now at ..."). If the list comes from another commit,
the counts are wrong. The script prints how many test functions in the
coverage data are not in the list, and warns when that number is not zero.
A small number can also come from tests that a decorator wraps (for example
`@respx.mock`): coverage then names them after the decorator's module, such
as `functools.test_x`, and the map loses them.

The report splits the changed files the map cannot judge into three kinds:
files the data does not have (for example files added after the coverage
run), files in the data that no test function ran, and other files (config,
test helpers, files outside `backend/`).

Things to know about the data:

- `test_function` records only lines that run inside the test function
  itself. Lines that run in fixtures, at import time, or in background tasks
  get the empty context, so the map does not see them.
- Coverage cannot use the faster `sysmon` core with test contexts. It falls
  back to the C tracer, so the shards run slower and need more time.

## When coverage collapses to ~24% — the invisible-import failure mode

**Symptom.** The SonarCloud overall-coverage chip drops sharply (e.g. from ~66%
to ~24%) and stays there, with no matching drop in real test coverage.

**What it means.** The nightly `python-coverage` job (ci.yml) died before
uploading `backend/coverage.xml`, so the Quality Gate scan ran *without* backend
coverage and SonarCloud imported ~0% for the backend tree — dragging the blended
Python+Go number down to roughly what Go coverage alone contributes. Because both
the coverage job and the artifact download are non-blocking
(`continue-on-error`), the failure produces no red check anywhere; the collapsed
chip is the only symptom.

**Worked example — the 2026-06-20 incident.** Last good import:
2026-06-19T18:55Z at **65.8%**. First collapsed import: 2026-06-20T10:46Z at
**24.9%** — and it stayed collapsed for 3+ weeks. Root cause: the
`python-coverage` job was OOM-killed / evicted (runner lost, null step
conclusions) at ~47 min on the memory-limited `meho-runners-ci-heavy` pool —
*before* its then-50-min job timeout (since #2800: a 45-min pytest step timeout
inside a 55-min job cap), so raising `timeout-minutes` is a no-op. The unit
lane had already dropped `--cov` for the same OOM reason (#1982), so this
dedicated job was the sole coverage producer and nothing else caught the gap.

**Where to look.**

1. The most recent `python-coverage` job on a nightly run (CI workflow, event
   `schedule`) — did it reach `Upload Python coverage artifact`, or die/OOM
   before it?
2. The Quality Gate run for that night — its `Assert python-coverage artifact
   present (fail-visible)` step emits an **error annotation** naming the missing
   artifact whenever the upload is absent (added in #2513). That annotation is
   the loud signal the 2026-06-20 collapse lacked.
3. The artifact list on the CI run — a present `python-coverage` artifact rules
   out this failure mode.

**The durable fix landed 2026-08-27 (#3172 / rdc-gitops#115).** The coverage
tax is intrinsic (~12.5 GiB peak at `-n 1`) and barely moves with parallelism —
and the "~1.2 GiB under the pod limit" this section used to claim was fiction:
the live heavy-pool runner limit was **8Gi** (13.67 GiB was #1982's measured
local peak, mistaken for the cap), so the sweep never fit and every main-push
coverage run since ~2026-06-20 died as an OOM runner-loss or a memory-thrash
wedge. rdc-gitops#115 resized the pool in place to **14Gi** (request==limit,
node-exclusive by memory on the 15.62-GiB-allocatable workers) — the job's
`runs-on:` stays. Real headroom is now ~1.5 GiB; if the suite outgrows
~13 GiB serial, the next lever is sharding the sweep into sequential pytest
invocations (`coverage combine` already merges across parallel data files),
not a bigger limit — 14Gi is this node size's ceiling. The job stays
non-blocking and the fail-visible annotation above surfaces every skipped
upload instead of letting it rot silently.

## When coverage reads about 36% with every check green — the missing worker file

**Symptom.** SonarCloud shows backend coverage far below the real number (36%
on `main` in October 2026, against about 87-93% with the same tests), and the
number jumps between pushes for no code reason. Unlike the ~24% case above,
the `python-coverage` artifact **is** there, every shard job is green, and the
Quality Gate run shows no error annotation.

**What it means.** One or more coverage shards uploaded data without the
file of their test worker. Each shard's pytest runs with `-n 1`: the pytest
main process only starts a worker and prints results; the worker runs the
tests. Coverage measures every process through `COVERAGE_PROCESS_START` and
writes a process's data file when that process exits. If the worker never
exits normally, its file is never written, and the shard only contributes
the code that runs at start-up (about 400 source files instead of about
1,000). The combine job merges whatever is there and reports a low number.
Nothing turns red because the coverage jobs are non-blocking by design.

**Why the worker does not exit normally.** Two causes, seen in October 2026
(#4758):

1. **The controller kills the worker too early.** When the worker reports
   "finished", the controller asks it to stop, execnet interrupts it after
   5 seconds (the `KeyboardInterrupt` traceback at the end of a shard log,
   usually inside weakref or `__del__` clean-up code), and pytest-xdist kills
   it 10 seconds after the request. Freeing a whole session's objects with
   coverage switched on can take longer than that. A killed worker writes
   nothing. **Fix:** the `pytest_sessionfinish` hook in `backend/tests/conftest.py`
   saves the worker's coverage data (and stops the tracer) before the worker
   reports "finished". Pinned by `backend/tests/test_ci_coverage_shards.py`.
2. **The shard hits its 30-minute step limit.** Then all of its processes are
   killed and every data file of that shard is lost (the shard uploads an
   empty or missing artifact). With 5 shards on the 7 GiB heavy runner, a
   shard with about 4,500 tests ran up to 29 minutes. **Fix:** the sweep runs
   as 8 shards (about 2,700 tests each). Raise the shard count again before
   any shard gets near the limit; the matrix, `TOTAL`, and the combine job's
   download steps must move together, and the same test pins that lockstep.

**Where to look.**

1. Download `python-coverage-data-shard-N` from the CI run (kept 1 day). The
   shard must hold one file of about 250 KB for the worker; the pytest main
   process file is about 230 KB, and helper processes leave files of about
   50 KB. To check one file, run in `backend/`:
   `uv run coverage report --rcfile=.coveragerc.ci --data-file=<file>`.
   The worker file alone reports about 60% for its shard; the main process
   file reports under 20%.
2. The shard's `coverage-pytest-log-shard-N` artifact (kept 7 days): a
   `KeyboardInterrupt` after the last test, or a step that stopped at the
   30-minute limit, means the shard lost its worker data.
3. The `Print peak memory` step of each shard: a peak at the memory limit
   or a pytest step near 30 minutes means the shard count must go up.

## New-code period + scan trigger — how they're configured

SonarCloud's "Clean as You Code" model gates only **changed** code, relative to a
configurable *new-code period*. Two decisions shape what the dashboard shows:

**New-code period = `previous_version` (SonarCloud default — kept deliberately).**
The tempting alternative — New Code = "Reference branch: `main`" — was considered
and rejected: because this project analyzes `main` (see the trigger below), pinning
the reference branch to `main` would diff `main` against **itself**, so main's
new-code window would be permanently ~empty and the CaYC signal would go dark on
the one branch that is actually analyzed. `previous_version` instead resets the
new-code window at each `sonar.projectVersion` bump (the scan stamps it with the
analyzed commit SHA — see `quality-gate.yml`'s `SonarCloud Scan` step), which keeps
"new code" meaningful across merges. The gate is left **advisory**
(`continue-on-error: true`) regardless.

> Historical note: the orphan-commit history reset once left the baseline
> predating every commit, so `new_lines` (~296k) exceeded total `ncloc` (~187k)
> and every `new_*` metric equalled its all-time twin. That artefact clears as
> `previous_version` windows accrue from real merges; no manual reference-branch
> setting is applied.

**Scan trigger = the nightly run only.** `quality-gate.yml`'s job carries
`if: … && github.event.workflow_run.event == 'schedule'`. CI runs on `push`,
`pull_request`, `merge_group` and once a night on `schedule` (`cron: '17 6 * * *'`
in `ci.yml`, #4910). SonarCloud stores a single analysis per project keyed by
branch — so before the event filter, every PR-branch CI completion triggered a
scan that **overwrote** the main-branch analysis, churning the activity history
and misaligning the new-code window with real merges. Since #4910 only the
nightly run produces the `python-coverage` artifact, so a scan of a push run
would import 0% backend coverage. With the filter, only the nightly run is
analyzed; push, PR and merge-queue completions are skipped (the job shows as
skipped in the Actions run). PR-level decoration is therefore not produced — an
accepted trade-off while the gate is advisory.

## Reading the dashboard without drowning

The single most useful triage axis is **test code vs. production code**. After the
`sonar.tests` split in `sonar-project.properties`, test fixtures no longer inflate
the ratings, but when reading raw issues remember:

- **Bugs**: historically *all* were in `backend/tests/**` (float `==` asserts).
  Production bug count was zero. Reliability E was 100% test noise.
- **Vulnerabilities**: ~87% were test fixtures (fake tokens, `/tmp`). The handful
  in production are reviewed below.
- **Maintainability** is genuinely **A** (debt ratio ~0.1%). Trust it.
- **Duplication** is concentrated in the Go per-vendor CLI command files
  (`cli/internal/cmd/<vendor>/<vendor>.go`, 44–74% each) — real, structural,
  tracked as the dispatch-boilerplate refactor.

The only smell bucket worth actively chasing is **`S3776` cognitive complexity**
(~45 functions, Python + Go); the high-volume buckets (`S7503` async-without-await,
`S1313` hard-coded IPs, `S8410` type-hints) are mostly intentional or test-only.

## Security hotspots — accepted false positives

28 hotspots; 25 are `S5332` clear-text-HTTP in **test** fixtures (bulk-mark *Safe*).
The three that touch production are all reviewed and **accepted**:

| Finding | Verdict |
|---|---|
| `go:S4036` PATH-exec of `gh` — `cli/internal/cmd/retrieval/retire_checklist.go` | **Safe.** Bounded-context `gh` invocation, already `//nolint:gosec`. You can't hardcode the path to `gh`. |
| `python:S5852` ReDoS — `backend/src/meho_backplane/operations/ingest/_llm_grouping_internals.py` | **Low risk.** ```` ```json ```` fence-stripper over bounded LLM output, not adversarial input. |
| `docker:S6504` group-write — `backend/Dockerfile` (`chmod -R g=u /opt/meho`) | **Safe.** Deliberate OpenShift / arbitrary-UID pattern: the container runs as any UID in group 0, and fastembed must *write* the model cache at runtime. Required, not gratuitous. |

Other accepted issue-level false positives: `go:S4830`/`S5527` on
`cli/internal/cmd/admin/keycloak/tls.go` (flag-gated `InsecureSkipVerify`, already
`//nolint:gosec`); `go:S2068` on the `keycloak.go` `--help` example string;
`python:S5443` on `bind9/_atomic.py` (CSPRNG-random `/tmp` name, runs as a remote
shell script). Mark these *Accepted* / *Won't fix* in SonarCloud with the rationale
above so the ratings reflect real risk.

The 8 production CRITICAL TLS findings are **by-design and suppressed in code**
via rule-scoped `# NOSONAR(Sxxxx)` comments (rule-scoped so only the named rule is
muted, not every issue on the line), each pointing at the module docstring that
carries the justification:

| Line | Rule | Why by-design |
|---|---|---|
| `connectors/net/tls.py:439` | S4423 | probe negotiates whatever the appliance offers; protocol version is *reported* output |
| `connectors/net/tls.py:440` | S4830 | inspection-only handshake; verification-off is the operation's purpose |
| `connectors/net/tls.py:441` | S5527 | hostname match is computed and *reported* by the handler, not enforced |
| `connectors/adapters/http.py:125` | S5527 | per-target `verify_tls=false` opt-out (default `True`); WARN-logged + audited; `tls_ca_pin` is the secure supersession |
| `connectors/adapters/http.py:127` | S4830 | same justification; see the module docstring's TLS-trust precedence |
| `connectors/vmware_rest/ovf_transfer.py:271` | S4830 | pre-flight cert-fetch handshake to the device host builds an unverified context; the lease `sslThumbprint` pin (`verify_device_thumbprint`) is the identity control (module docstring #3229/#3284) |
| `connectors/vmware_rest/ovf_transfer.py:272` | S5527 | same handshake; the device host is self-signed, so hostname match is not the control — the pin fails closed on a cert mismatch |
| `connectors/vmware_rest/ovf_transfer.py:273` | S4830 | same handshake; `CERT_NONE` fetches the cert to hash for the pin, it is not the trust decision |

These are deliberately **not** muted project-wide (no source-tree multicriteria on
the TLS rules) so future genuinely-insecure TLS code still gets flagged.

## Real backlog (not noise)

- **Go CLI dispatch boilerplate** — extract a shared `CallOperation` helper so the
  ~18 per-vendor command files stop re-implementing the HTTP-to-`/operations/call`
  plumbing. Compatible with CLAUDE.md postulate 5 (transport DRYing ≠ thin wrapper).
- **K8s resource requests/limits** (`kubernetes:S6870`) on the chart deployments +
  migration job.
- **Dockerfile hardening** (`docker:S8541`).
- *(optional)* **`S3776` cognitive-complexity** pass on the worst offenders.
