# Self-hosted GitHub Actions runners (`meho-x-runners`)

Operational reference for the self-hosted runner pool that backs MEHO.X CI.
Covers what the pool is, what runs on it, what does *not*, the auth model,
day-2 operations, and known performance characteristics.

## Why this pool exists

GitHub-hosted runners are billed per-minute. CI on MEHO.X runs on every PR
and every push to `main`, and the heavy jobs (Python unit tests + mypy)
each burn 3-7 minutes per run. The self-hosted pool exists to **move that
cost off GitHub-hosted billing** onto infrastructure we already pay for.

Cost is the only driver. We are not using self-hosted runners for any of
the other usual reasons (GPUs, VPC access, larger machines, restricted
network egress) — they are stock Linux x86_64 boxes that happen to be ours.

## What you get

| Property | Value |
|---|---|
| Runner label | `meho-x-runners` |
| Architecture | x86_64 Linux |
| Auth model | Classic PAT (org-scope `admin:org`), TBD owner |
| Provisioning | TBD — fill in (cloud provider, region, IaC tool, host count) |
| Image / base OS | TBD — fill in (Ubuntu 24.04? custom AMI?) |
| Pre-installed tooling | Whatever the runner image ships with — actions install Python / Node / `uv` per job |

The runner pool registers at the **organization** level (`evoila-bosnia`),
so any repo in the org can target it via `runs-on: meho-x-runners` once
the workflow opts in.

## What currently runs on it

As of 2026-05-04:

- [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — all 6 jobs
  (`lint-python`, `lint-typescript`, `typecheck`, `test-python`,
  `test-frontend`, `test-scripts`).

Every other workflow (`pr-checks`, `frontend-tests`, `quality-gate`,
`dead-code-check`, `license-check`, `secret-scan`, `security-scan`,
`mirror-to-public`, `release`, `cla`, `planning-guard`,
`pat-expiration-probe`, `arm64-first-run`) still uses `ubuntu-latest`.

The migration plan is to move every cost-sensitive workflow to
`meho-x-runners` over time. See [Migration policy](#migration-policy).

## Migration policy

Move a workflow to `meho-x-runners` when **all** of these hold:

- It runs frequently enough that GitHub-hosted minutes meaningfully
  contribute to the bill (PR checks, scheduled scans, anything that
  fires on every push).
- It does not need a runner property the pool lacks (arm64, GPU,
  Windows, macOS, IP allowlist, etc.).
- It does not fall under [What must *not* run on this pool](#what-must-not-run-on-this-pool).

Leave on `ubuntu-latest` if any of those fail. In particular,
[`arm64-first-run.yml`](../../.github/workflows/arm64-first-run.yml)
explicitly needs `ubuntu-24.04-arm` and stays on GitHub-hosted.

## What must *not* run on this pool

Self-hosted runners are a [known privilege-escalation
surface](https://www.praetorian.com/blog/self-hosted-github-runners-are-backdoors/).
Treat the pool as production infrastructure that an attacker would love
to land on.

**Do not put on `meho-x-runners`:**

1. **Any workflow that runs untrusted code from forks.** MEHO.X is
   currently a private repo, so this is dormant — but the moment we
   open a public mirror that accepts external PRs, any `pull_request`
   workflow on the public side must stay on GitHub-hosted runners (or
   be gated behind `pull_request_target` with explicit safeguards).
2. **Workflows that handle release artifacts signed with org-level
   secrets** without an extra layer of isolation. The runner host can
   read every secret a job is granted — keep release signing on
   ephemeral GitHub-hosted runners until we have ephemeral-per-job
   self-hosted runners (ARC).
3. **Workflows where a compromise of the runner host would be hard to
   detect.** Same logic as above. If in doubt, leave it on
   `ubuntu-latest`.

When introducing a new workflow, default to `ubuntu-latest` and flip
to `meho-x-runners` deliberately after reading this section.

## Auth

The runner host uses a **classic PAT with `admin:org` scope**, owned
by an `evoila-bosnia` org owner, to fetch short-lived registration
tokens from the GitHub REST API. The PAT itself never leaves the
runner host; it's used only at registration / re-registration time to
call `POST /orgs/evoila-bosnia/actions/runners/registration-token`.

Why classic PAT and not fine-grained:

- Fine-grained PATs *can* register self-hosted runners (permission
  `Self-hosted runners: Read and write`), but only when created by an
  org owner. They also still trip on org-level PAT-approval policies
  and SSO-authorization gates that classic PATs sail through. Classic
  PAT is the path of least resistance.
- We accepted that this means manual rotation when the PAT expires.
  See [Rotating the PAT](#rotating-the-pat).

A future migration to a **GitHub App** is intentionally deferred. An
App installation token would eliminate rotation, scope auth to a
single purpose, and is what GitHub recommends for ARC-style setups.
We will revisit this once the pool stabilizes — until then, the PAT
is fine.

### Rotating the PAT

> [!TODO]
> Fill in the actual rotation runbook once owner + secrets-store
> location are confirmed. Skeleton:
>
> 1. Org owner generates a new classic PAT with `admin:org` scope.
>    Authorize SSO if `evoila-bosnia` enforces it.
> 2. Update the PAT in `<secrets-store-location>` (1Password vault?
>    GitHub org secret? Pulumi config?).
> 3. Re-run the runner provisioning step (`<command>`) on each host
>    in the pool. The runner re-registers with a fresh registration
>    token.
> 4. Revoke the old PAT.
> 5. Confirm CI still passes by retriggering a recent PR.

## Day-2 operations

> [!TODO]
> Most of this section is placeholder pending the actual provisioning
> details. Fill in once owner is identified.

### Adding a node to the pool

TBD.

### Removing a node

TBD — outline:

1. Drain — wait for any in-flight job to finish.
2. `./config.sh remove --token <REG_TOKEN>` on the host (uses a
   fresh registration token, same endpoint as registration).
3. Tear down the host.

### Updating the runner binary

GitHub deprecates old runner versions on a rolling schedule. The
`actions/runner` agent self-updates by default unless explicitly
disabled — confirm self-update is on so we don't hit a hard cutoff.

### Where the runner lives / who owns it

TBD — fill in: cloud provider, region, host names, IaC repo, the
on-call rotation that gets paged when CI starts queueing.

## Known performance characteristics

Measured on the first run after introducing `meho-x-runners` (PR #561,
2026-05-04). Apples-to-apples comparison: same commit content, same
job definitions, just `runs-on:` flipped.

| Job | `ubuntu-latest` | `meho-x-runners` | Δ |
|---|---|---|---|
| Shell Script Tests (bats) | 24s | 24s | 0% |
| Frontend Unit Tests | 1m58s | 2m02s | +3% |
| TypeScript Lint (ESLint) | 42s | 56s | +33% |
| Python Lint (Ruff) | 28s | 1m06s | +136% |
| Type Check (mypy + tsc) | 3m37s | 6m23s | +77% |
| Python Unit Tests | 3m46s | 6m44s | +79% |
| **Wall-clock (max of parallel jobs)** | **3m46s** | **6m44s** | **+79%** |

The slowdown concentrates on jobs that do `uv sync` or `npm ci` from
cold caches. `actions/setup-python` and `actions/setup-node` rely on
on-runner cache directories that are warm out-of-the-box on
GitHub-hosted runners and start empty on a freshly provisioned
self-hosted host. **Subsequent runs on the same host should improve**
as those caches populate.

If after a few days of normal CI traffic the heavy jobs are still
~80% slower than the GitHub-hosted baseline, investigate:

- Are the cache directories persisted across jobs (or wiped on every
  run by an over-eager teardown)?
- Is the runner backed by slow / network-attached disk that thrashes
  during `uv sync`?
- Is outbound bandwidth to PyPI / npm registry bottlenecking?
- Would a `uv` package mirror or shared package cache mount help?

## References

- [GitHub Docs — REST API for self-hosted runners](https://docs.github.com/en/rest/actions/self-hosted-runners)
- [GitHub Docs — Authenticating ARC to the GitHub API](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners-with-actions-runner-controller/authenticating-to-the-github-api)
- [Praetorian — Self-Hosted GitHub Runners are Backdoors](https://www.praetorian.com/blog/self-hosted-github-runners-are-backdoors/)
- [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — first
  workflow to run on the pool
