# JSONFlux — the vendored set-shaped-response reducer

The `JsonFluxReducer` is MEHO's production answer to CLAUDE.md postulate 6
and v0.1-spec §4: no agent ever sees a 4 MB raw API response. Any
operation returning a set-shaped payload above threshold is materialized
into an in-memory DuckDB table, summarized, and replaced with a
[`ResultHandle`](operations-substrate.md#jsonflux-integration); agents
drill in via the `result_*` meta-tools.

The reduction engine is **vendored** — copied into this repo rather than
pulled as a PyPI dependency — because the upstream is a single
unversioned commit and the load-bearing copy is MEHO.X's fork. This doc
records the provenance, the license chain, what diverges from MEHO.X, the
adapter that wires the vendored tree into the dispatcher, and the runbooks
for keeping the copy in step (upstream-sync) or flowing fixes back
(reverse-sync).

## Source provenance

The vendored tree lives at
[`backend/src/meho_backplane/jsonflux/`](../../backend/src/meho_backplane/jsonflux/).
It is a verbatim copy (headers aside — see [License](#license)) of the
MEHO.X fork:

- **Vendor source:** MEHO.X `meho_app/jsonflux/` at commit **`fc82cf93`**
  (`fix(types): remove 65 unused type: ignore comments across 38 files`,
  2026-04-01) — the latest commit touching `meho_app/jsonflux/` and the
  latest mypy-clean state of that tree. Vendored by T2 (#752).
- **Upstream origin:** `github.com/ikaric/jsonflux` at commit **`da85962`**
  (`"Initial commit"`). This is the *only* commit on upstream — no tags, no
  version history. The code declares `__version__ = "1.0.0"` with no
  matching git tag; the MEHO.X fork downgraded its own `__version__` to
  `"0.1.0"` (verifiable at
  [`jsonflux/__init__.py`](../../backend/src/meho_backplane/jsonflux/__init__.py))
  to signal "this is a fork, not the unversioned upstream".

The provenance chain is therefore:

```
ikaric/jsonflux (da85962, MIT)
        │  forked into MEHO.X
        ▼
MEHO.X meho_app/jsonflux/ (fc82cf93, AGPL-3.0-only — 22 fork commits)
        │  vendored into evoila/meho
        ▼
backend/src/meho_backplane/jsonflux/ (Apache-2.0 — this repo)
```

The license-path decision that gates this vendoring is recorded in the T1
ADR: [`docs/decisions/jsonflux-license.md`](../decisions/jsonflux-license.md).

## License

The vendored tree ships under **Apache-2.0**, uniform with the rest of
`evoila/meho`. Every vendored file carries the repo-standard two-line
SPDX header:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
```

This is **Option B** of the T1 ADR: @ikaric — who is both the original
`ikaric/jsonflux` (MIT) author *and* the author of the MEHO.X
(`AGPL-3.0-only`) fork — relicensed the MEHO.X-adjusted code to Apache-2.0
for vendoring here. The signoff of record and the full option analysis
(why not re-import MIT upstream, why not an MIT carve-out) live in the
ADR: [`docs/decisions/jsonflux-license.md`](../decisions/jsonflux-license.md).
The signoff is **not** re-pasted here — the ADR is the single source of
truth for it.

Attribution is recorded in the repo-root [`NOTICE`](../../NOTICE) file,
which credits @ikaric / `ikaric/jsonflux` as the upstream origin and names
the `fc82cf93` vendor pin.

### Why the SPDX header differs from MEHO.X

MEHO.X ships `# SPDX-License-Identifier: AGPL-3.0-only` on every jsonflux
file (added in MEHO.X commits `84-01` / `100-01`). Apache-2.0 **cannot
ship AGPL-3.0 code** — the copyleft would contaminate the whole
backplane. So T2 replaced the `AGPL-3.0-only` header with `Apache-2.0` on
every vendored file. Only the SPDX line changed; the
`# Copyright (c) 2026 evoila Group` line was already present on the MEHO.X
files and was kept. This relicensing is what required @ikaric's signoff —
see the ADR. **Do not "fix" these headers back to AGPL or MIT on a future
sync** — the AGPL-header gotcha is exactly what the ADR exists to prevent
recurring.

## MEHO.X divergence catalog

The vendored tree is **not** upstream `ikaric/jsonflux`. MEHO.X shipped 22
jsonflux-touching commits since the fork, diverging across six categories.
Five are quality-bar hygiene that `evoila/meho` would regress on if it
re-imported from the MIT upstream; only one is functional. Restating the
catalog from Initiative #750 §"Why the import policy needs a ticket":

| # | Category | Kind | MEHO.X commit(s) |
|---|---|---|---|
| 1 | Smart `register()` with `unwrap='auto'\|True\|False` + `append=True` shape detection | 🟢 functional | `8f48c141 feat(68.1-01)` |
| 2 | mypy zero-error pass (36 jsonflux + 22 core errors) | 🟡 hygiene | `106-04`, `108-01`, `fc82cf93` |
| 3 | ruff zero-error pass (188 fixes + per-file ignore policy) | 🟡 hygiene | `105-01` |
| 4 | SonarQube cognitive-complexity suppressions (~10 hot funcs) | 🟡 hygiene | `107-03`, `107-07` |
| 5 | Module-level string-constant extraction (S1192) | 🟡 hygiene | `107-03` |
| 6 | SPDX headers `AGPL-3.0-only` on every file | 🔴 relicensed | `84-01`, `100-01` |

Category 6 is the one T2 *reversed* (AGPL → Apache-2.0); see
[License](#license). Categories 2–5 are why the policy is "vendor the fork
verbatim", not "re-import upstream and re-apply" — re-applying ~500 net
line-changes across 22 commits by hand is the path most likely to silently
drop a fix.

### The functional divergence: smart `register()`

Category 1 is the only behaviorally-different code. The smart `register()`
on
[`QueryEngine`](../../backend/src/meho_backplane/jsonflux/query/engine.py)
adds two parameters over upstream and detects the response *shape* before
materializing it — load-bearing for vendor APIs that wrap their
collections in envelopes (vCenter REST, NSX, SDDC Manager, K8s, Vault all
do).

The verbatim signature:

```python
def register(
    self,
    name: str,
    source: str | Path | dict | list,
    path: str | None = None,
    description: str | None = None,
    unwrap: bool | str = "auto",   # 'auto' | True | False
    append: bool = False,          # concatenate vs. replace same-named table
) -> QueryEngine: ...
```

With `unwrap="auto"` (the default the reducer uses), `register()`
classifies a dict source into one of **four shape-detection paths** —
exercised by the four shapes vendor list ops actually emit:

```python
from meho_backplane.jsonflux.query.engine import QueryEngine

eng = QueryEngine()

# Path 1 — flat array: [{...}, ...]  ->  one multi-row table.
eng.register("vms", [{"id": "vm-1"}, {"id": "vm-2"}])
# table "vms": 2 rows

# Path 2 — wrapped collection: {"results": [...], "total": N}  ->
#   the list becomes the main table; scalars/nested objects spill into a
#   "{name}_meta" companion table (latest page wins on append).
eng.register("nsx", {"results": [{"id": "seg-1"}], "result_count": 1})
# table "nsx": 1 row;  table "nsx_meta": 1 row (result_count=1)

# Path 3 — single flat object: {"k": "v"}  ->  1-row table,
#   tier_hint="inline" (it is metadata, not a collection).
eng.register("vm", {"power_state": "POWERED_ON", "cpu_count": 4})
# table "vm": 1 row, inline

# Path 4 — multi-collection: {"pods": [...], "svcs": [...]}  ->
#   split into "{name}_pods" and "{name}_svcs" tables.
eng.register("k8s", {"pods": [{"name": "a"}], "svcs": [{"name": "b"}]})
# table "k8s_pods": 1 row;  table "k8s_svcs": 1 row
```

`unwrap=True` forces the first-list-of-dicts-value path regardless of
shape; `unwrap=False` forces a 1-row table. The detection itself lives in
`QueryEngine._detect_and_split(...)`; the four-path contract is documented
verbatim on the `register()` docstring.

> **Note on list-of-scalars.** `register(unwrap="auto")` classifies a
> *list of scalars* (Vault's `{"keys": ["a", "b", ...]}`) as metadata and
> collapses it to a 1-row table — not one row per key. The
> [reducer adapter](#reducer-adapter) compensates by normalizing
> list-of-scalars to `[{"value": "a"}, ...]` rows *before* calling
> `register()`, so the row count (the threshold input) is correct for
> every vendor shape. See `_normalize_rows` in `jsonflux_reducer.py`.

## Reducer adapter

The dispatcher does not call the vendored package directly. The bridge is
[`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`](../../backend/src/meho_backplane/operations/jsonflux_reducer.py),
shipped by T3 (#753). It structurally satisfies the
[`Reducer` Protocol](operations-substrate.md#jsonflux-integration) (the
Protocol is `@runtime_checkable`, so no explicit inheritance) and is
installed as the production default at app startup via
`set_default_reducer(JsonFluxReducer())` in
[`main.py`](../../backend/src/meho_backplane/main.py). The module-level
fallback in the dispatcher remains `PassThroughReducer()`; the running app
overwrites it with `JsonFluxReducer`.

The adapter drives the lower-level `QueryEngine` directly (not the
`JsonFlux` facade) because only `QueryEngine` exposes the smart
`register(unwrap=...)`. Per `reduce(payload, schema, context)`: it
detects the primary collection (envelope key or bare list), normalizes
list-of-scalars to row dicts, checks the threshold, and — when over —
registers the rows in a fresh in-memory DuckDB table, builds a JSON-Schema
(Draft 2020-12) `schema_` dict from the DuckDB `DESCRIBE`, renders a
markdown summary, samples N rows, and returns
`(summary_dict, ResultHandle)`. Small / scalar payloads return
`(payload, None)` unchanged.

### Constructor defaults

Read off the shipped adapter
([`jsonflux_reducer.py`](../../backend/src/meho_backplane/operations/jsonflux_reducer.py)):

| Param | Default | Meaning |
|---|---|---|
| `row_threshold` | `50` | Materialize when the detected collection has **more than** this many rows. `0` forces materialization for every non-empty set (force / test mode). |
| `byte_threshold` | `4096` | Materialize when the serialized payload exceeds this many bytes, even if under `row_threshold`. |
| `sample_size` | `5` | Rows surfaced inline on the `ResultHandle.sample_rows` preview and in the markdown summary. `0` returns no sample. |
| `ttl_seconds` | `3600` | Lifetime stamped onto `ResultHandle.ttl_seconds` for the backing store. |

All four are keyword-only. The defaults match v0.1-spec §4 (50 rows /
4 KB). Empty collections never materialize — a 0-row handle carries no
information a pass-through doesn't.

For how the dispatcher invokes the reducer (the `try/except` wrap that
turns a reducer exception into `connector_error` with audit + broadcast
still firing, and the `ResultHandle` shape), see
[`operations-substrate.md` §JSONFlux integration](operations-substrate.md#jsonflux-integration).

## Upstream-sync runbook

How to pull future MEHO.X jsonflux changes into the vendored copy. The
pin this runbook syncs *from* is recorded in
[Source provenance](#source-provenance): **`fc82cf93`**.

1. **Find new commits since the pin.** In the attached MEHO.X checkout:

   ```bash
   git -C /Users/damirtopic/repos/evoila-bosnia/MEHO.X fetch origin
   git -C /Users/damirtopic/repos/evoila-bosnia/MEHO.X \
     log --oneline fc82cf93..origin/main -- meho_app/jsonflux/
   ```

   Empty output → nothing to sync; stop here. Otherwise each line is a
   commit to apply, oldest first.

2. **List the full file-level history** (sanity-check which files moved,
   and catch any *new* files MEHO.X added that need a fresh SPDX header):

   ```bash
   git -C /Users/damirtopic/repos/evoila-bosnia/MEHO.X \
     log --oneline --stat fc82cf93..origin/main -- meho_app/jsonflux/
   ```

3. **Apply each new commit** to `backend/src/meho_backplane/jsonflux/`.
   Cherry-pick is rarely clean across the two repos (different roots,
   different SPDX headers), so the reliable path is per-file diff-and-apply:

   ```bash
   # For each changed file, diff the MEHO.X range and apply by hand or
   # via `git apply --3way` after rewriting the path prefix:
   git -C /Users/damirtopic/repos/evoila-bosnia/MEHO.X \
     diff fc82cf93..origin/main -- meho_app/jsonflux/query/engine.py
   ```

   **Re-apply the Apache-2.0 SPDX header to any new file** MEHO.X added
   (MEHO.X ships `AGPL-3.0-only`; this repo must carry — see
   [License](#license)):

   ```python
   # SPDX-License-Identifier: Apache-2.0
   # Copyright (c) 2026 evoila Group
   ```

   Never copy a MEHO.X `AGPL-3.0-only` header through unchanged.

4. **Re-run the type + lint gates** against the vendored tree:

   ```bash
   cd backend
   uv run mypy src/meho_backplane/jsonflux/
   uv run ruff check src/meho_backplane/jsonflux/
   uv run ruff format --check src/meho_backplane/jsonflux/
   ```

   New lint exceptions → update the per-file ignore blocks in
   [`backend/pyproject.toml`](../../backend/pyproject.toml) (the
   `[tool.ruff.lint.per-file-ignores]` block keyed under
   `src/meho_backplane/jsonflux/*`, and the
   `[[tool.mypy.overrides]]` block for `meho_backplane.jsonflux.*`). The
   existing carveouts document *why* each rule is suppressed; extend, do
   not blanket-ignore.

5. **Run the test suite** — both the adapter tests and any force-mode
   handle tests that assert real materialization shape:

   ```bash
   cd backend
   uv run pytest tests/
   ```

   If `register()` behavior changed upstream, the adapter's
   collection-detection assumptions
   ([`jsonflux_reducer.py`](../../backend/src/meho_backplane/operations/jsonflux_reducer.py))
   may need a matching update — the adapter normalizes list-of-scalars and
   reads `engine.tables[...]["row_count"]`, so a rename there is breaking.

6. **Update the pin and commit.** Bump the `fc82cf93` SHA to the new
   MEHO.X SHA in **three** places, then commit:

   - the [Source provenance](#source-provenance) section of this doc,
   - the [`NOTICE`](../../NOTICE) attribution entry,
   - the comment in
     [`backend/pyproject.toml`](../../backend/pyproject.toml) per-file
     ignore block (`# Vendored JSONFlux tree (#752, ... at fc82cf93)`).

   The Initiative #750 body carries the canonical source-pin field — note
   the new SHA there too if the Initiative is still open.

### Validation against current MEHO.X HEAD

Steps 1–2 were run against MEHO.X `main` HEAD at vendor time. As of this
doc, `git log fc82cf93..origin/main -- meho_app/jsonflux/` returns **no
commits** — `fc82cf93` is still the latest commit touching the jsonflux
tree, so there is nothing to sync. The vendored copy is current.

## Reverse-sync runbook

When a fix lands in `evoila/meho`'s vendored tree that should also flow
back to MEHO.X (and possibly upstream `ikaric/jsonflux`), the policy is
**document the chain, don't enforce it** — this is in-scope to write down,
out-of-scope to automate.

1. **Extract the diff** for the fix, scoped to the vendored tree:

   ```bash
   git -C /path/to/evoila/meho \
     format-patch -1 <fix-sha> -- backend/src/meho_backplane/jsonflux/
   ```

2. **Rewrite the path prefix** from
   `backend/src/meho_backplane/jsonflux/` to MEHO.X's
   `meho_app/jsonflux/`, and **swap the SPDX header back** to MEHO.X's
   license (`AGPL-3.0-only`) on any new file — the inverse of the
   upstream-sync header rewrite. Do **not** carry the Apache-2.0 header
   into MEHO.X.

3. **Open the fix as a PR on MEHO.X** (`evoila-bosnia/MEHO.X`), crediting
   the chain on the commit so authorship survives the round-trip:

   ```
   Co-Authored-By: <original MEHO.X / ikaric author> <email>
   Co-Authored-By: <evoila/meho fix author> <email>
   ```

   The original MEHO.X author (resolve via
   `git -C /path/to/MEHO.X log -- meho_app/jsonflux/<file>`) is credited
   so the fork→upstream→fork loop keeps a continuous attribution chain. If
   the fix is also relevant to `ikaric/jsonflux` upstream, the same
   `Co-Authored-By` chain applies to that PR.

4. **Note the back-port in this repo.** Once the MEHO.X PR merges, the
   vendored copy and MEHO.X are back in step — a subsequent upstream-sync
   (above) will see the back-ported commit as already-applied; skip it to
   avoid a no-op double-apply.

## References

- T1 ADR (license decision): [`docs/decisions/jsonflux-license.md`](../decisions/jsonflux-license.md)
- Dispatcher integration + `Reducer` Protocol + `ResultHandle` shape:
  [`operations-substrate.md` §JSONFlux integration](operations-substrate.md#jsonflux-integration)
- Reducer adapter: [`backend/src/meho_backplane/operations/jsonflux_reducer.py`](../../backend/src/meho_backplane/operations/jsonflux_reducer.py)
- Vendored tree: [`backend/src/meho_backplane/jsonflux/`](../../backend/src/meho_backplane/jsonflux/)
- Attribution: repo-root [`NOTICE`](../../NOTICE)
- Parent Initiative [#750](https://github.com/evoila/meho/issues/750) —
  §"Why the import policy needs a ticket" (the divergence catalog source).
- Upstream origin (provenance only): `github.com/ikaric/jsonflux`
  (single commit `da85962`, MIT).
- v0.1-spec §"JSONFlux / result handles" L294-311:
  <https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/v0.1-spec.md>
