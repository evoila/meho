# Compatibility gate for the REST API, the MCP tools and the Helm values

The CI workflow `.github/workflows/compat-gates.yml` stops a pull request
that breaks clients of the REST API, the MCP tools or the Helm values. A breaking change can
still merge, but only when the pull request says so on purpose: it has the
`breaking-change` label and a `## Breaking changes` section that tells
clients what to change.

## Overview

Three sets of files describe what clients depend on:

| File | What it holds | Kept equal to the code by |
| --- | --- | --- |
| `cli/api/openapi.json` | The REST API. Only paths under `/api/v1/` carry the promise; the console paths under `/ui/` do not. | the `ci.yml` job `cli-api-snapshot-freshness` |
| `backend/contracts/mcp-tools-list.json` | Every MCP tool: its name, its surface (`working` or `operator`), its input schema and its output schema. Reader text (`description`, `title`, `examples`, `$comment`) is left out. | `backend/tests/test_mcp_tools_snapshot_drift.py` |
| `deploy/charts/meho/values.yaml` and `values.schema.json` | The Helm values an operator sets. | the chart itself |

The workflow compares each file with the same file on the base branch and
lists every breaking change.

## Control flow

1. A pull request to `main` opens, gets a new commit, gets or loses a label,
   or gets new text. Each of these runs the job again.
2. The job checks out the merge commit. `HEAD^1` is the base branch.
3. It installs `oasdiff` (pinned version, checked against its sha256).
4. `scripts/ci/check_contract_compat.py --base HEAD^1` runs all checks:
   - **REST API:** `oasdiff breaking --fail-on ERR --match-path '^/api/v1/'`.
     Only `oasdiff`'s definite breaking changes (`error` level) count. Its
     warnings are not shown and do not fail the job.
   - **MCP tools:** the script's own rules (below).
   - **Helm values:** a key that `values.yaml` had at the base and no longer
     has is breaking (lists count as one value). `values.schema.json` is
     compared with the rules for an MCP input schema (below).
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

A schema without `type` accepts any type. So adding a `type` to such an
argument is breaking, and removing the `type` of an output field is
breaking too. An integer is a number: an argument that accepted `integer`
and accepts `number` now is not breaking.

The rules look into nested `properties`, array `items`, and the branches of
`oneOf` and `anyOf`. Branches are paired by content, not by position:
equal branches first, then each branch left over takes the closest one
(the fewest differences, then the nearest position). So a change of their
order is not breaking. The index in a message is the position in the new
schema. A branch removed from an input, or a new branch in an output, is
breaking.

`oneOf` in an input gets one more rule. A value must match exactly one
`oneOf` branch. So when a new or widened branch accepts a value that another
branch accepts too, that value matched before and is rejected now. That is
breaking. The check compares what two branches both accept before and after
the change, and reports only what is new: two branches that both took `null`
before may keep doing so, but may not both start to take strings. A branch
with an `enum` accepts only the values it lists, so two `enum` branches
overlap only on a value both list, and an `enum` branch overlaps a plain
branch only when one of its values has a type the plain branch accepts. An
integer is also a number, and a branch without `type` accepts every type.
Enum values compare the JSON Schema way: `1` and `1.0` are the same value
(an integer), `true` is not `1`, and the string `"1"` is not the number `1`.
`anyOf` has no such rule: more branches only accept more.

The schema of the Helm values uses the input rules: a removed property, a
newly required property, a narrower type or a removed enum value is
breaking. A local `$ref` (such as `#/definitions/httpProbe`) is replaced
by the definition it points to before the compare, so a change inside a
definition counts at every place that uses it. A `$ref` that points to
nothing stops the check (exit 2). The schema's `if` / `then` / `allOf`
rules and `additionalProperties` are not compared.

A new tool, a new optional argument, a new output field and a new enum
value in an input are not breaking.

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
- Not covered yet: the CLI verbs and flags, and environment variables.
  The update from the previous release is tested only for the Helm install
  (the job "Upgrade from the last release" in `.github/workflows/chart.yml`).
  The other install forms have no such test yet.
- The Helm check sees keys and the schema, not a changed default or a
  changed template. The golden renders (below) show those in review, but
  they do not fail a pull request as breaking.

## Golden renders of the chart

`deploy/golden/*.yaml` hold the output of `helm template` for three fixed
sets of values: `built-in-core`, `bring-your-own` and `openshift`. The step
"Check the golden renders of the chart" in the `validate` job of
`.github/workflows/chart.yml` runs `scripts/ci/helm_golden.py`. It fails
when a golden file is not equal to the render. Fix it with:

```bash
python scripts/ci/helm_golden.py --write
```

and commit the files. The three values that change on every render (two
generated passwords and the checksum after one) are written as
`<random>`. A new generated value needs a new rule in `mask_random`. The
script needs `helm`; the test is `backend/tests/test_ci_helm_golden.py`.

## References

- `scripts/ci/check_contract_compat.py` and its tests
  `backend/tests/test_ci_check_contract_compat.py`.
- `backend/scripts/snapshot_mcp_tools.py` and its test
  `backend/tests/test_mcp_tools_snapshot_drift.py`.
- oasdiff: <https://github.com/oasdiff/oasdiff> (`docs/BREAKING-CHANGES.md`,
  `docs/FILTERING-ENDPOINTS.md`).
