# Release-body path-freshness gate

The `scripts/release/check_release_body_paths.py` gate asserts that
every `/api/v*` path cited in a proposed GitHub release body resolves
in the published OpenAPI snapshot. Sister gate to
`cli-api-snapshot-freshness` (#928) — that one runs at PR time and
catches "OpenAPI snapshot lags the route table"; this one runs at
release time and catches "release body cites a path the route table
doesn't expose".

## Why this exists

Three consecutive release cycles shipped with broken path citations
in the release body. The pattern is cross-cycle and stable, not a
per-cycle slip:

- **v0.5.0**: CHANGELOG `[Unreleased]` → `[0.5.0]` roll skipped
  entirely. `cli-release.yml` fell back to extracting `[Unreleased]`
  (empty), and the GitHub Release was hand-curated without an audit
  against shipped routes.
- **v0.5.1**: release body said the new connector raw-REST on-ramp
  answered v0.3.0's "only 13 vmware ops?" — but the path it gave
  operators was the read-only catalog (`/api/v1/connectors/catalog`,
  introduced by #743), not live typed-connector dispatch.
- **v0.6.0**: release body cited `GET /api/v1/audit/replay` (actual
  shipped path: `GET /api/v1/audit/sessions/{session_id}/replay`,
  introduced by #1012) and described "6 tenant-scoped + RBAC-gated
  API routes" + references to `tenant_conventions` under a `tenant-`
  prefix that doesn't exist (actual mount: `/api/v1/conventions`
  with 3 routes, not 6). Also: the topology history endpoint cited
  as `/api/v1/topology/history` (actual: per-resource
  `/api/v1/topology/history/{name}`).

The release runbook (`docs/RELEASING.md`) makes "roll the CHANGELOG
before tagging" load-bearing post-v0.5.0, but a load-bearing CHANGELOG
isn't sufficient: the v0.6.0 cycle rolled the CHANGELOG correctly
and *still* shipped with drifted paths because no automated check
read the prose against the OpenAPI surface.

## Mechanism

The script is pure-Python stdlib (`argparse` / `json` / `re`) — no
backplane import, no test-collection coupling. It's safe to run from
any working directory.

1. **Extract** every `/api/v*` token from the release-body markdown.
   The regex `r"/api/v[0-9]+/[A-Za-z0-9_/{}\-.]*"` matches the
   widest legal-looking URL path; trailing punctuation (`.`, `,`,
   `;`, etc.) is stripped post-match. `}` is preserved as a
   legitimate trailing char for OpenAPI templates.
2. **Templatise** each citation. Segments shaped like a UUID
   (`[0-9a-f]{8}-...`) or a pure-digit ID are replaced with the
   parameter-name pool the OpenAPI snapshot uses at that segment
   position (e.g. `{session_id}` for the 5th segment of an
   `/api/v1/audit/sessions/<X>/replay` path). The original literal
   form is kept as a candidate too, so a literal OpenAPI path
   matches without surgery.
3. **Match** every candidate against the set of `paths` keys in
   the OpenAPI snapshot (`cli/api/openapi.json` — the artifact
   `make snapshot-openapi` produces). At least one candidate must
   match; otherwise the citation is unresolved.
4. **Report** unresolved citations on stderr with the closest
   snapshot-path hint, ranked by (shared-prefix length, last-segment
   match). Exit 1 when any citation is unresolved.

Template-awareness is what makes the gate useful. Without it, a
release body that legitimately writes a concrete example URL —
`/api/v1/audit/sessions/abc-123-.../replay` — would trip the gate
against the templated snapshot path. Strict literal matching would
have produced too many false positives to keep the gate on.

## Where it runs

- **Release runbook (`docs/RELEASING.md` §3)** — operator runs it
  on the candidate release body before merging the release-cutting
  PR. The `/release` skill (`evoila-bosnia/meho-internal`) drives
  the runbook end-to-end; the skill update tracking the new step
  lands as a separate meho-internal PR per the
  `project_claude_dir_symlink` discipline.
- **Author's local loop** — the script accepts any release-body
  markdown file, so authors can run it against draft prose before
  cutting the PR. Recommended when amending an existing release
  body retroactively (the same shape this Task takes for v0.6.0).

## Whitelist

The `--allow-path` flag accepts a path the gate should treat as
resolved even when absent from the snapshot. Use cases:

- Forward-looking citations: a v0.6 release body that mentions a
  v0.7 path landing next minor.
- Paths served by a sibling service (not the FastAPI backplane).

Every whitelist entry is a tech-debt marker — the right move is
usually to amend the release body to cite the shipped path, not
to suppress the gate. The flag is for the genuine intentional-
out-of-snapshot citation, not for "make the gate stop complaining."

## Limitations

- **Markdown-only.** The gate reads the release body as text; it
  doesn't parse HTML or JSON inside code blocks. A code-block
  example that happens to contain `/api/v1/foo` will be extracted
  and checked alongside prose citations. In practice that's a
  feature (a stale example URL is still drift) but worth noting.
- **No CHANGELOG-section coverage.** The CHANGELOG follows the
  release body pattern (`cli-release.yml` extracts the release
  body *from* the CHANGELOG section), so fixing the release body
  fixes the CHANGELOG transitively. Expanding the gate to scan
  every `## [X.Y.Z]` section across CHANGELOG history was
  explicitly out of scope for #1136.
- **No call into the backplane.** The gate has no Python imports
  outside stdlib, so it can run before `uv sync` (during a release
  bootstrap), and it does not couple to backplane test collection.

## References

- **Sister gate at PR time:** `cli-api-snapshot-freshness` job in
  `.github/workflows/ci.yml` (introduced by #928).
- **Snapshot generator:** `cli/api/snapshot-openapi.py` (driven by
  `make snapshot-openapi`).
- **Release runbook:** `docs/RELEASING.md` §3.
- **Originating Task:** #1136 (G0.13-T6).
- **Originating Initiative:** #1130 (G0.13 v0.6.0 dogfood hardening).
- **Project memory:** `project_release_runbook`,
  `project_ci_openapi_snapshot_freshness`.
