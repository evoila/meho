# Compatibility gate for the REST API and the MCP tools

The CI workflow `.github/workflows/compat-gates.yml` stops a pull request
that breaks clients of the REST API or the MCP tools. A breaking change can
still merge, but only when the pull request says so on purpose: it has the
`breaking-change` label and a `## Breaking changes` section that tells
clients what to change.

## Overview

Two committed files describe what clients depend on:

| File | What it holds | Kept equal to the code by |
| --- | --- | --- |
| `cli/api/openapi.json` | The REST API. Only paths under `/api/v1/` carry the promise; the console paths under `/ui/` do not. | the `ci.yml` job `cli-api-snapshot-freshness` |
| `backend/contracts/mcp-tools-list.json` | Every MCP tool: its name, its surface (`working` or `operator`), its input schema and its output schema. Reader text (`description`, `title`, `examples`, `$comment`) is left out. | `backend/tests/test_mcp_tools_snapshot_drift.py` |

The workflow compares each file with the same file on the base branch and
lists every breaking change.

## Control flow

1. A pull request to `main` opens, gets a new commit, gets or loses a label,
   or gets new text. Each of these runs the job again.
2. The job checks out the merge commit. `HEAD^1` is the base branch.
3. It installs `oasdiff` (pinned version, checked against its sha256).
4. `scripts/ci/check_contract_compat.py --base HEAD^1` runs both checks:
   - **REST API:** `oasdiff breaking --fail-on ERR --match-path '^/api/v1/'`.
     Only `oasdiff`'s definite breaking changes (`error` level) count. Its
     warnings are not shown and do not fail the job.
   - **MCP tools:** the script's own rules (below).
5. No breaking change: the job passes. A breaking change: the job lists it.
   It passes only when the pull request has the `breaking-change` label and
   a `## Breaking changes` section with text under the heading.

### What counts as breaking for an MCP tool

- The tool is gone.
- The tool moved from the working surface to the operator surface (a normal
  session no longer lists it).
- Input schema (what the client sends): an argument is gone, an argument is
  now required, an argument accepts fewer types, or an enum value is gone.
- Output schema (what the client reads): a field is gone, a field is no
  longer always returned (`required`), a field changed type, or the whole
  output schema is gone.

The rules look into nested `properties` and array `items`. A new tool, a
new optional argument, a new output field and a new enum value in an input
are not breaking.

### When the check cannot run

The script fails closed (exit code 2) when `oasdiff` is missing or cannot
read a file, when the base ref does not exist, or when the MCP snapshot is
not valid JSON. When a snapshot file does not exist on the base yet, that
part is skipped with a note. This happens only on the first pull request
that adds the file.

## How to fix a failed run

- **You did not mean to break anything:** change the code so it stays
  compatible. For example, keep the old argument and add the new one next
  to it.
- **The break is on purpose:** add the `breaking-change` label and a
  `## Breaking changes` section to the pull request text. Write the
  smallest change a client must make. The release pull request copies this
  into the CHANGELOG (code pull requests do not edit `CHANGELOG.md`).
- **The MCP snapshot is out of date** (the drift test fails): run
  `cd backend && uv run python scripts/snapshot_mcp_tools.py` and commit
  the file. Never edit it by hand, and never merge it by hand: after a merge
  of `main`, run the script again.

## Before a release candidate

Run the same check against the previous tag, on the candidate commit:

```bash
gh workflow run compat-gates.yml --repo evoila/meho --ref main \
  -f base="$RC_PREV" -f head="$RC_SHA"
```

A manual run has no label, so every breaking change fails it. Each listed
break needs a `Breaking changes` line in the CHANGELOG. See
`docs/RELEASING.md`, "R3 — Before cutting an rc".

## Known gaps

- The job does not run in the merge queue: labels are not visible there.
  Add a merge queue path before the check becomes required.
- Not covered yet: Helm values (golden renders of the chart), the update
  from the previous release, the CLI verbs and flags, and environment
  variables.

## References

- `scripts/ci/check_contract_compat.py` and its tests
  `backend/tests/test_ci_check_contract_compat.py`.
- `backend/scripts/snapshot_mcp_tools.py` and its test
  `backend/tests/test_mcp_tools_snapshot_drift.py`.
- oasdiff: <https://github.com/oasdiff/oasdiff> (`docs/BREAKING-CHANGES.md`,
  `docs/FILTERING-ENDPOINTS.md`).
