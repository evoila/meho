# Contributing to MEHO

Thanks for your interest in contributing to MEHO. This project ships
in the open, runs against real infrastructure on every change, and
takes review seriously. The sections below describe the development
loop, the conventions every PR is held to, and how the two-repo
coordination model works.

## How we develop: the dogfood loop

`evoila/meho` (this repo — public, all code) and
`evoila-bosnia/claude-rdc-hetzner-dc` (private, the operator-side
manifests and runbooks for the RDC Hetzner dogfooding lab) are coupled.
Every feature ships first to the dogfooding consumer; operator feedback
loops back into `evoila/meho` via PRs. The discipline is: features
merge against **real-target deploys**, not mocks.

When you propose a feature touching the deploy contract (image, chart,
or CLI), expect a coupled discussion thread on the consumer repo —
that's not a side conversation, it's how we measure done. The
[`deploy/values-examples/values-rdc-example.yaml`](./deploy/values-examples/values-rdc-example.yaml)
in this repo is the sanitized template; the actual live values file
lives on the consumer side and only flips green once both sides agree
the contract is right.

## Public from day 1, deliberately

This repo is OSS and public from commit #1. We never quarantine work
here under "private until ready" reasoning. Either it ships in public
from the start, or it goes to `evoila-bosnia/meho-internal` (planning
issues, ADRs, sensitive operator material only — **no code**).

If you're nervous about something being "not ready enough" for public —
file a **draft PR**. The review thread is the conversation, not a
release. The codebase walkthroughs at
[`docs/codebase/`](./docs/codebase/) are the closest thing we have to
"here's what shipped so far"; they're updated as part of the PR that
introduces the change, never after the fact.

## Bidirectional coordination with claude-rdc-hetzner-dc

For features that touch the deploy contract (image, chart, CLI),
expect this flow:

1. Issue filed on `evoila-bosnia/meho-internal` (the planning repo).
   Goal → Initiative → Task hierarchy per project 19; the Task is
   what a PR closes.
2. Sibling issue filed on `evoila-bosnia/claude-rdc-hetzner-dc`
   capturing the consumer-side prerequisites (kubeconfig, RBAC,
   ExternalSecret manifests, …).
3. PR opens against `evoila/meho` with `Closes #<task>` referencing
   the planning issue.
4. PR merges only when CI is green **and** the consumer-side ticket
   has the manifests merged that the new chart version requires.

For features that are purely backplane-internal (audit format,
federation internals, observability wiring), no consumer-side ticket
is needed — but a heads-up in the consumer's `CLAUDE.md` "MEHO
transition" section is courteous.

## Development setup

The repo ships three independently buildable artefacts. Each has a
local-dev loop documented in its own README; this section is the
index.

| Artefact | Language | Local-dev README |
| --- | --- | --- |
| Backplane | Python 3.13 + FastAPI | [`backend/README.md`](./backend/README.md) — `uv sync`, `docker compose up`, pytest layout |
| Helm chart | YAML + Helm 3 | [`deploy/charts/meho/`](./deploy/charts/meho/) — `helm template`, `helm lint`; [`deploy/values-examples/README.md`](./deploy/values-examples/README.md) covers the install flow |
| Operator CLI | Go 1.23+ | [`cli/README.md`](./cli/README.md) — `go build`, oapi-codegen wiring, `meho version` smoke |

To exercise the full deploy contract on your laptop, follow the
**Deploy → Local (kind, ~5 min)** section in [`README.md`](./README.md).
That path skips real Vault + Keycloak (operator identity is faked) but
runs the chart's install plumbing — pre-install migration Job, the
Deployment, the Valkey broadcast subchart — against a real Kubernetes
API.

For real federation work, the dogfooding lab on RDC Hetzner is the
target — coordinate via the consumer-side ticket.

## Pull request flow

1. **Branch from `main`.** Convention: `feat/<issue#>-<slug>`,
   `fix/<issue#>-<slug>`, `chore/<slug>`, etc. The `<issue#>` is the
   planning issue on `evoila-bosnia/meho-internal`.
2. **Make your change.** Stay in scope — adjacent findings go into
   the PR description, not stealth commits.
3. **Sign every commit** with `git commit -s` (see
   [Developer Certificate of Origin](#developer-certificate-of-origin)).
4. **Conventional Commits** in the subject:
   `feat(connector): ...`, `fix(policy): ...`, `chore(ci): ...`,
   `docs(readme): ...`. The pre-commit `conventional-pre-commit`
   hook enforces this on `commit-msg`.
5. **Reference the planning issue** in the PR body with
   `Closes #<task-number>` (or `Refs #<n>` for partial work).
6. **Push, open a PR** targeting `main` on `evoila/meho`.
7. **CI must pass** — every required check is green; no overrides.
8. **CodeRabbit review** posts automatically on every PR; address the
   findings or document why a finding is wrong inline.
9. **Maintainer review** approves the human side. Reviews on
   `evoila/meho` are mandatory; reviews on `evoila-bosnia/meho-internal`
   PRs (planning-only, ADRs, governance docs) are best-effort because
   those PRs touch no code.
10. **Squash merge.** The PR title (which becomes the merge commit
    subject) is what shows up in `git log` on `main`.

## Code style

Each language has a fixed toolchain. CI runs the same tools — no
"works on my machine" surprises.

- **Python (backplane, tests):** `ruff check` + `ruff format` are the
  format-and-lint pair. `mypy --ignore-missing-imports` for
  type-checking. Pinned versions live in
  [`backend/pyproject.toml`](./backend/pyproject.toml) under the
  `dev` group; the pre-commit hook (when it lands) will use the
  same pinned versions.
- **Go (operator CLI):** `gofmt -s` + `golangci-lint run` from
  [`cli/.golangci.yml`](./cli/.golangci.yml) (when the file lands;
  Task #43 scaffolded the module). `go vet` is part of the same
  pipeline. Generated code (oapi-codegen output) is checked in and
  excluded from lint.
- **YAML, Markdown, JSON:** trailing-whitespace, end-of-file-fixer,
  and `check-yaml` / `check-json` from the pre-commit hooks repo
  catch the common formatting traps. CI also runs `gitleaks` for
  secret detection on every PR.

The full pre-commit config lives at
[`.pre-commit-config.yaml`](./.pre-commit-config.yaml). Install hooks
locally with `pre-commit install --install-hooks` (and re-run them on
the whole tree with `pre-commit run --all-files` if you need to
verify a clean baseline).

## Developer Certificate of Origin

Every commit must carry a `Signed-off-by: Your Name <your@email>`
trailer. Use:

```bash
git commit -s -m "feat(scope): your message"
```

`git commit -s` adds the trailer automatically using your configured
`user.name` and `user.email` (set these once globally with
`git config --global user.name "..."` /
`git config --global user.email "..."`).

The [DCO bot](https://github.com/apps/dco) checks every PR on
`evoila/meho`. A missing `Signed-off-by:` on any commit fails the
`DCO` required check, which blocks merge under main branch protection.

Backfilling a sign-off after-the-fact requires rewriting history — for
a single missing commit:

```bash
git commit --amend --signoff
git push --force-with-lease
```

For a multi-commit PR, an interactive rebase with
`git rebase --signoff <base>` re-signs every commit in the range. Both
flows are noisy; the easier discipline is to make `-s` the default.

There is no CLA. Apache 2.0 §5 ("inbound = outbound": contributions
flow in under the same Apache 2.0 terms the project ships under) plus
the DCO trailer is the full contributor agreement. The license ADR
documenting this choice lives at ADR 0001 (license selection) and
ADR 0002 (CLA-vs-DCO).

## Code of conduct

See [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).

## Reporting bugs

Use the issue templates at
<https://github.com/evoila/meho/issues/new/choose>. For bugs that
relate to the deploy contract (chart misbehaviour, CLI flag
regression, image start-failure), attach: chart version, image tag,
`kubectl get pods -n meho`, and the relevant pod logs.

For broader proposals (new connector, new policy primitive, deploy
flow change), the right route is a planning issue on
`evoila-bosnia/meho-internal` — that repo holds the
Goal → Initiative → Task hierarchy under project 19, and a PR on
`evoila/meho` closes one of the Tasks.

## Security

Vulnerabilities: see [`SECURITY.md`](./SECURITY.md). Do not file
public issues for security problems — the file documents the
private reporting channel.
